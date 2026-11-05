#!/usr/bin/env node
// Checks the proxy's goal bridge: a "/goal ..." prompt from the client is
// answered by the agent's native goal API instead of being sent to the model.
//
//   node test_mcode_acp_goal_bridge.mjs [proxy] [launcher]
import { spawn } from 'node:child_process';
import readline from 'node:readline';

const PROXY = process.argv[2] || '/Users/prince/.minimax-code/bin/mcode-acp-proxy.js';
const LAUNCHER = process.argv[3] || '/Users/prince/.minimax-code/releases/0.7.4/.mcode-launcher';
let failures = 0;

function check(label, ok, detail) {
  console.log(`${ok ? 'ok  ' : 'FAIL'}  ${label}${detail ? ' :: ' + detail : ''}`);
  if (!ok) failures++;
}

const child = spawn(process.execPath, [PROXY, LAUNCHER, 'acp'], { stdio: ['pipe', 'pipe', 'inherit'] });
let nextId = 1;
const pending = new Map();
const chunks = [];
const notifications = [];
const rl = readline.createInterface({ input: child.stdout, crlfDelay: Infinity });
rl.on('line', (line) => {
  let msg;
  try { msg = JSON.parse(line); } catch { return; }
  if (msg.method === 'session/update') {
    const u = msg.params?.update;
    if (u?.sessionUpdate === 'agent_message_chunk') {
      const c = u.content;
      chunks.push(c?.text ?? (c?.content ?? []).map((x) => x.text ?? '').join(''));
    }
    return;
  }
  if (msg.method) { notifications.push(msg.method); return; }
  if (msg.id !== undefined && msg.id !== null && msg.method === undefined) {
    const p = pending.get(msg.id);
    if (p) { pending.delete(msg.id); p(msg); }
  }
});
const request = (method, params) => new Promise((resolve) => {
  const id = nextId++;
  pending.set(id, resolve);
  child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
});

await request('initialize', { protocolVersion: 1, clientCapabilities: {} });
const created = await request('session/new', { cwd: process.cwd(), mcpServers: [] });
const sessionId = created.result.sessionId;

// 1. Create a goal through the native API.
const create = await request('session/prompt', {
  sessionId,
  prompt: [{ type: 'text', text: '/goal Verify the native goal bridge from the proxy. Scope: the proxy only. Verify: the goal shows up in the agent\'s native goal API.' }],
});
check('the goal command answered the client', !create.error, create.error ? JSON.stringify(create.error) : JSON.stringify(create.result));
check('the goal command did not become a model turn', create.result?.stopReason === 'end_turn', JSON.stringify(create.result));
const said = chunks.join('');
check('the client was told about the goal', /goal .* created/i.test(said), said.slice(-160));

// 2. The goal must exist in the agent's native goal API.
const get = await request('mcode/session/goal/get', { sessionId });
check('the native goal API has the goal', !get.error && Boolean(get.result?.goal), get.error ? JSON.stringify(get.error) : JSON.stringify(get.result?.goal?.goalId));
check('the goal objective is the one from the command',
  (get.result?.goal?.objective ?? '').includes('native goal bridge'),
  (get.result?.goal?.objective ?? '').slice(0, 80));

// 3. Status through the bridge.
chunks.length = 0;
const status = await request('session/prompt', { sessionId, prompt: [{ type: 'text', text: '/goal status' }] });
check('the status command answered', !status.error && status.result?.stopReason === 'end_turn', JSON.stringify(status.result ?? status.error));
check('the status command reported the goal', /is active/i.test(chunks.join('')), chunks.join('').slice(-160));

// 4. Clear it again.
chunks.length = 0;
const clear = await request('session/prompt', { sessionId, prompt: [{ type: 'text', text: '/goal stop' }] });
check('the stop command answered', !clear.error && clear.result?.stopReason === 'end_turn', JSON.stringify(clear.result ?? clear.error));
const after = await request('mcode/session/goal/get', { sessionId });
check('the goal is gone', !after.result?.goal, JSON.stringify(after.result?.goal ?? null));

// 5. The prompt as T3 Code sends it: the user's message wrapped in
//    instruction blocks. The command still has to be found.
chunks.length = 0;
const wrapped = [
  '<system-reminder>',
  'Use `tool_search` to discover permitted on-demand tools.',
  '</system-reminder>',
  '',
  '<plugin-hook-context>',
  'Goal Mode is active for this session.',
  'Call goal_get with view="summary" and report the current goal state.',
  '</plugin-hook-context>',
  '',
  '<user_request>',
  '/goal Wrapped command must still create a goal.',
  '</user_request>',
].join('\n');
const wrappedGoal = await request('session/prompt', { sessionId, prompt: [{ type: 'text', text: wrapped }] });
check('a wrapped goal command is answered', !wrappedGoal.error && wrappedGoal.result?.stopReason === 'end_turn', JSON.stringify(wrappedGoal.result ?? wrappedGoal.error));
const wrappedState = await request('mcode/session/goal/get', { sessionId });
check('the wrapped command created a goal', Boolean(wrappedState.result?.goal), JSON.stringify(wrappedState.result?.goal?.goalId));
check('the objective is the user request, not the wrapper',
  (wrappedState.result?.goal?.objective ?? '').includes('Wrapped command must still create a goal'),
  (wrappedState.result?.goal?.objective ?? '').slice(0, 80));
if (wrappedState.result?.goal) {
  await request('mcode/session/goal/clear', { sessionId });
}

// 6. An ordinary prompt whose instruction blocks talk about goals must reach
//    the model instead of being taken for a goal command.
chunks.length = 0;
const ordinary = [
  '<plugin-hook-context>',
  'Goal Mode is active for this session.',
  'Call goal_get with view="summary" and report the current goal state.',
  '</plugin-hook-context>',
  '',
  '<user_request>',
  'Summarise the goal state machine in this repository in one sentence.',
  '</user_request>',
].join('\n');
const ordinaryResult = await request('session/prompt', { sessionId, prompt: [{ type: 'text', text: ordinary }] });
check('the ordinary prompt was answered', !ordinaryResult.error, JSON.stringify(ordinaryResult.error ?? ordinaryResult.result));
const afterOrdinary = await request('mcode/session/goal/get', { sessionId });
check('the ordinary prompt did not create a goal', !afterOrdinary.result?.goal, JSON.stringify(afterOrdinary.result?.goal?.goalId));
check('the ordinary prompt reached the model', chunks.join('').length > 0, chunks.join('').slice(-100));

console.log(failures === 0 ? '\nall checks passed' : `\n${failures} check(s) failed`);
child.kill();
process.exit(failures === 0 ? 0 : 1);
