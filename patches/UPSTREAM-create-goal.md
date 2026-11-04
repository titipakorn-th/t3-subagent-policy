# Upstream request: expose `create_goal` to the agent

**Short version:** the runtime defines a `create_goal` tool, never wires it into the
agent's tool set, and the version that is defined would create a goal that never
runs. The working creation path today is the ACP extension, which the proxy uses.

## What the code says

`@minimax-ai/code` 0.7.4, `chunks/chunk-66K6XTTT.js`:

```js
var OO = { name: "create_goal", executionMode: "sequential", description: "Create a goal only when explicitly requested ...", schema: ... };
var STi = (() => { let e = [Ut(OO)], ... constructor(s, o) { this.store = s; this.onChanged = o; } ... })();
```

The module is built, and the file's export list contains `ITi as qe` (update_goal)
and `kTi as re` (get_goal) - but **not** `STi`. Nothing imports it, so the agent
never receives the tool.

The registration site is `chunks/chunk-YYSTRBDA.js`:

```js
function tdt(t, e, r, n = {}) {
  return e ? [] : [CV(new spe(t, r, n.budgetMutation, n.limitsOf)), CV(new ape(t, n.limitsOf))];
}
```

`spe` is the update_goal module, `ape` the get_goal module. There is no third entry.

## Why exporting it is not enough

`STi.execute` creates the goal through the **store**:

```js
let d = await this.store.create(l);
return this.onChanged?.(d), BK(OO.name, { goal: DO(d) })
```

The runtime's own goal service does considerably more (same file, `createGoal`):

```js
if (!await this.deps.sessionExists(e.sessionId)) throw ...;
if (await this.deps.gates.isGoalVerifierChildSession(e.sessionId)) throw ...;
if (await this.deps.gates.hasAutomationOwnerConflict(e.sessionId)) throw ...;
let n = await hae(this.deps.objectiveAssets, e.sessionId, e.objectiveResources, i => this.deps.store().create({...e, objectiveResources: i}));
if (this.deps.runtime.emitRuntimeEvent({ type: "goal.created", ... }), !r.requireKickoffAdmission) return await this.handleCreated(n, r.sendBehavior), n;
try { await this.deps.continuation.startInitial(n, false, r.sendBehavior); } catch (i) { throw await this.deps.continuation.rollbackFailedCreation(n), i; }
...
this.deps.continuation.requestQueueDispatch(n.sessionId)
```

The store alone does not gate the goal, emit the event, admit the kickoff, start the
continuation, or request the queue dispatch. A goal created through the tool would
sit in the database, `active`, and never run - and the session would then refuse a
second goal ("this thread already has a goal with status active").

The tool's dependencies are only `store`, `signalCollector` and `limitsOf`
(`runtimeToolsFor`), so the service is not reachable from there either.

## What we would like

Either:

1. the tool calls the same goal service the ACP extension calls
   (`mcode/session/goal/create` -> `runtime.createGoal`), or
2. the tool is removed, so the ACP extension is the only creation path.

Option 1 is the useful one: an agent that receives "keep working until the tests
pass" should be able to start that itself, with the same gates and kickoff the ACP
path has.

## What we did instead

The proxy turns a `/goal ...` prompt into the ACP extension call
(`mcode/session/goal/create`), which goes through the full service, gates and
kickoff included. That path is verified: the goal runs on its own and closes
itself.

Files: `patches/test_mcode_acp_goal_bridge.mjs` (the bridge),
`patches/test_mcode_acp_goal_continuation.mjs` (the goal running on its own).
