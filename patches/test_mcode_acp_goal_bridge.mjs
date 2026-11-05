#!/usr/bin/env node
// Checks the proxy's goal bridge: a "/goal ..." prompt from the client is run
// by the proxy as visible prompt turns.
//
// The agent's native goal API cannot be used here: mcode runs a goal's turns
// in a child session and never projects them to the client, so the work would
// be invisible in T3. The proxy keeps the client's request open and runs the
// goal as ordinary turns, so the client sees the work while it happens.
//
//   node test_mcode_acp_goal_bridge.mjs [proxy] [launcher]
import { spawn } from 'node:child_process';
import fs from 'node:fs';
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

// 1. A goal the client can watch.
const marker = '/tmp/mcode-goal-bridge-marker.txt';
fs.rmSync(marker, { force: true });
chunks.length = 0;
const goal = await request('session/prompt', {
  sessionId,
  prompt: [{ type: 'text', text: `/goal Write the single line BRIDGE_OK to ${marker} and stop.` }],
});
check('the goal command answered the client', !goal.error, JSON.stringify(goal.error));
check('the goal command ended the turn', goal.result?.stopReason === 'end_turn', JSON.stringify(goal.result));
const said = chunks.join('');
check('the client saw the agent work', said.length > 0, said.slice(-200));
check('the work was done', fs.existsSync(marker) && fs.readFileSync(marker, 'utf8').includes('BRIDGE_OK'), marker);

// 2. No native goal: it would do the same work again, invisibly.
const native = await request('mcode/session/goal/get', { sessionId });
check('no native goal was left running', !native.result?.goal, JSON.stringify(native.result?.goal?.goalId ?? null));

// 3. The status of the goal that just ran.
chunks.length = 0;
const status = await request('session/prompt', { sessionId, prompt: [{ type: 'text', text: '/goal status' }] });
check('the status command was answered', !status.error && status.result?.stopReason === 'end_turn', JSON.stringify(status.result ?? status.error));
check('the status command reported the goal', /goal is (met|running|stopped)/i.test(chunks.join('')), chunks.join('').slice(-160));

// 4. The prompt as T3 Code sends it: the user's message wrapped in
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
  '/goal Reply with the single word WRAPPED and stop.',
  '</user_request>',
].join('\n');
const wrappedGoal = await request('session/prompt', { sessionId, prompt: [{ type: 'text', text: wrapped }] });
check('a wrapped goal command is answered', !wrappedGoal.error && wrappedGoal.result?.stopReason === 'end_turn', JSON.stringify(wrappedGoal.result ?? wrappedGoal.error));
check('the wrapped command started a goal', /WRAPPED/i.test(chunks.join('')), chunks.join('').slice(-200));

// 5. An ordinary prompt whose instruction blocks talk about goals must reach
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
check('the ordinary prompt reached the model', chunks.join('').length > 0, chunks.join('').slice(-100));

fs.rmSync(marker, { force: true });
console.log(failures === 0 ? '\nall checks passed' : `\n${failures} check(s) failed`);
child.kill();
process.exit(failures === 0 ? 0 : 1);
