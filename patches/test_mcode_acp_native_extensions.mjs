#!/usr/bin/env node
// Checks the mcode-native ACP extension API that T3 Code does not use:
//
//   mcode/session/steer        - steer the running turn (native, no cancel+prompt hack)
//   mcode/session/queue/*      - queue a message for later
//   mcode/session/goal/*       - native goal management
//   _meta opt-in               - turns on the mcode/session/* notifications
//
//   node test_mcode_acp_native_extensions.mjs [proxy] [launcher]
import { spawn } from 'node:child_process';
import readline from 'node:readline';

const PROXY = process.argv[2] || '/Users/prince/.minimax-code/bin/mcode-acp-proxy.js';
const LAUNCHER = process.argv[3] || '/Users/prince/.minimax-code/releases/0.7.4/.mcode-launcher';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let failures = 0;

function check(label, ok, detail) {
  console.log(`${ok ? 'ok  ' : 'FAIL'}  ${label}${detail ? ' :: ' + detail : ''}`);
  if (!ok) failures++;
}

const child = spawn(process.execPath, [PROXY, LAUNCHER, 'acp'], { stdio: ['pipe', 'pipe', 'inherit'] });
let nextId = 1;
const pending = new Map();
const answers = [];
const notifications = new Set();
const rl = readline.createInterface({ input: child.stdout, crlfDelay: Infinity });
rl.on('line', (line) => {
  let msg;
  try { msg = JSON.parse(line); } catch { return; }
  if (msg.method === 'session/update') {
    const u = msg.params?.update;
    if (u?.sessionUpdate === 'agent_message_chunk') {
      const c = u.content;
      const txt = c?.text ?? (c?.content ?? []).map((x) => x.text ?? '').join('');
      if (txt) answers.push(txt);
    }
    return;
  }
  if (msg.method) { notifications.add(msg.method); return; }
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

const init = await request('initialize', {
  protocolVersion: 1,
  clientCapabilities: {
    fs: { readTextFile: true, writeTextFile: true },
    terminal: true,
    _meta: { 'minimax-code/extensions': { version: 1, notifications: true } },
  },
});
check('initialize with the extension opt-in', !init.error, init.error ? JSON.stringify(init.error) : '');

const created = await request('session/new', { cwd: process.cwd(), mcpServers: [] });
const sessionId = created.result.sessionId;
check('session/new', Boolean(sessionId), sessionId);

// 1. Native steer while a turn runs.
const first = request('session/prompt', {
  sessionId,
  prompt: [{ type: 'text', text: 'Use the bash tool to run exactly: sleep 20; echo LONG_DONE . Then reply with the word DONE.' }],
});
await sleep(3000);
const steer = await request('mcode/session/steer', { sessionId, text: 'Reply with exactly NATIVE_STEER_OK.' });
check('native steer accepted', !steer.error, steer.error ? `${steer.error.code} ${steer.error.message}` : JSON.stringify(steer.result));
const steerRes = await first;
const said = answers.join('');
check('native steer reached the model', said.includes('NATIVE_STEER_OK'), said.slice(-100));
check('the steered turn still reports a stop reason', Boolean(steerRes.result?.stopReason), JSON.stringify(steerRes.result));

// 2. Native queue.
const enq = await request('mcode/session/queue/enqueue', { sessionId, text: 'Queued message from the native queue API.' });
check('queue/enqueue', !enq.error, enq.error ? `${enq.error.code} ${enq.error.message}` : JSON.stringify(enq.result));
const qlist = await request('mcode/session/queue/list', { sessionId });
check('queue/list', !qlist.error, qlist.error ? JSON.stringify(qlist.error) : JSON.stringify(qlist.result));
const itemId = qlist.result?.items?.[0]?.itemId;
if (itemId) {
  const del = await request('mcode/session/queue/delete', { sessionId, itemId });
  check('queue/delete', !del.error, del.error ? JSON.stringify(del.error) : JSON.stringify(del.result));
}

// 3. Native goal API.
const goal = await request('mcode/session/goal/create', { sessionId, objective: 'Verify the native goal API is reachable.' });
check('goal/create', !goal.error, goal.error ? `${goal.error.code} ${goal.error.message}` : JSON.stringify(goal.result));
if (!goal.error) {
  const gget = await request('mcode/session/goal/get', { sessionId });
  check('goal/get', !gget.error && Boolean(gget.result?.goal), JSON.stringify(gget.result));
  const gclear = await request('mcode/session/goal/clear', { sessionId });
  check('goal/clear', !gclear.error, gclear.error ? JSON.stringify(gclear.error) : JSON.stringify(gclear.result));
}

// 4. Delegation snapshot.
const deleg = await request('mcode/session/delegation/get', { sessionId });
check('delegation/get', !deleg.error, deleg.error ? `${deleg.error.code} ${deleg.error.message}` : 'ok');

console.log('\nagent->client notifications seen:', [...notifications].join(', ') || '(none)');
console.log(failures === 0 ? '\nall checks passed' : `\n${failures} check(s) failed`);
child.kill();
process.exit(failures === 0 ? 0 : 1);
