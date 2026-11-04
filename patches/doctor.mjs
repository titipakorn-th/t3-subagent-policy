#!/usr/bin/env node
// Checks that the mcode ACP setup on this machine is complete.
//
//   node patches/doctor.mjs [proxy] [launcher]
//
// The roll-out is spread over several files, so a machine can look patched and
// still be missing a piece. This runs the real agent and reports each part.
import { spawn } from 'node:child_process';
import readline from 'node:readline';
import fs from 'node:fs';
import path from 'node:path';

const HERE = path.dirname(new URL(import.meta.url).pathname);
const PROXY = process.argv[2] || '/Users/prince/.minimax-code/bin/mcode-acp-proxy.js';
const LAUNCHER = process.argv[3] || '/Users/prince/.minimax-code/releases/0.7.4/.mcode-launcher';
const FIXTURE = path.join(HERE, 'mcode-acp-proxy-steer.js');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let failures = 0;
function check(label, ok, detail) {
  console.log(`${ok ? 'ok  ' : 'FAIL'}  ${label}${detail ? ' :: ' + detail : ''}`);
  if (!ok) failures++;
}

// 1. The installed proxy.
if (!fs.existsSync(PROXY)) {
  check('the proxy exists', false, PROXY);
  process.exit(1);
}
const proxySource = fs.readFileSync(PROXY, 'utf8');
check('the proxy carries the fix', proxySource.includes('RETRYABLE_REJECTION'), PROXY);
if (fs.existsSync(FIXTURE)) {
  check('the proxy matches the known-good file', proxySource === fs.readFileSync(FIXTURE, 'utf8'));
}

// 2. The agent behind it.
const child = spawn(process.execPath, [PROXY, LAUNCHER, 'acp'], { stdio: ['pipe', 'pipe', 'inherit'] });
let nextId = 1;
const pending = new Map();
const chunks = [];
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

const init = await request('initialize', { protocolVersion: 1, clientCapabilities: {} });
check('the agent answers initialize', !init.error, init.error ? JSON.stringify(init.error) : '');

const extensions = init.result?._meta?.['minimax-code/extensions']?.methods ?? [];
for (const method of ['mcode/session/steer', 'mcode/session/goal/create', 'mcode/session/queue/enqueue']) {
  check(`the agent advertises ${method}`, extensions.includes(method));
}

const created = await request('session/new', { cwd: process.cwd(), mcpServers: [] });
const sessionId = created.result?.sessionId;
check('the session starts', Boolean(sessionId), sessionId ?? '');
check('the goal command is advertised to the client',
  (created.result?.slashCommands ?? []).some((c) => c.name === 'goal'),
  JSON.stringify((created.result?.slashCommands ?? []).map((c) => c.name)));

// 3. The goal API, end to end.
chunks.length = 0;
const goal = await request('session/prompt', {
  sessionId,
  prompt: [{ type: 'text', text: '/goal Doctor check: create a goal, then remove it.' }],
});
check('a goal command is answered', !goal.error && goal.result?.stopReason === 'end_turn', JSON.stringify(goal.result ?? goal.error));
const state = await request('mcode/session/goal/get', { sessionId });
check('the goal reached the native store', Boolean(state.result?.goal), JSON.stringify(state.result?.goal?.goalId));
if (state.result?.goal) {
  await request('mcode/session/goal/clear', { sessionId });
  const after = await request('mcode/session/goal/get', { sessionId });
  check('the goal can be cleared again', !after.result?.goal);
}

console.log('');
console.log(failures === 0 ? 'everything checks out' : `${failures} check(s) failed`);
console.log('restart T3 Code (or open a new thread) for the proxy changes to take effect');
child.kill();
process.exit(failures === 0 ? 0 : 1);
