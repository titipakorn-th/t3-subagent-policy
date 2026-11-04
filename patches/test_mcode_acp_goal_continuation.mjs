#!/usr/bin/env node
// Does the native goal do the work on its own, the way the plugin did?
//
// Creates a goal through the bridge and then waits. The goal kicks off its
// own turn: the file it was asked to create is the evidence that it worked
// without the client asking for anything.
//
//   node test_mcode_acp_goal_continuation.mjs [proxy] [launcher]
import { spawn } from 'node:child_process';
import readline from 'node:readline';
import fs from 'node:fs';

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
const messages = [];
const notifications = [];
const rl = readline.createInterface({ input: child.stdout, crlfDelay: Infinity });
rl.on('line', (line) => {
  let msg;
  try { msg = JSON.parse(line); } catch { return; }
  if (msg.method === 'session/update') {
    const u = msg.params?.update;
    if (u?.sessionUpdate === 'agent_message_chunk') {
      const c = u.content;
      const txt = c?.text ?? (c?.content ?? []).map((x) => x.text ?? '').join('');
      if (txt) messages.push({ at: Date.now(), text: txt });
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

// A goal with a concrete, checkable objective.
await request('session/prompt', {
  sessionId,
  prompt: [{ type: 'text', text: '/goal Keep working until the file /tmp/mcode-goal-continuation.txt exists and contains the word CONTINUED. Verify by reading the file back.' }],
});
const goal = await request('mcode/session/goal/get', { sessionId });
console.log('goal:', goal.result?.goal?.goalId, '| status:', goal.result?.goal?.status);

// One ordinary turn, then watch for automatic continuation.
const before = messages.length;
const turn = await request('session/prompt', {
  sessionId,
  prompt: [{ type: 'text', text: 'Write the text CONTINUED into /tmp/mcode-goal-continuation.txt using the bash tool, then reply with DONE.' }],
});
console.log('turn result:', JSON.stringify(turn.result ?? turn.error));
const turnEndedAt = Date.now();

await sleep(60000);
const after = messages.filter((m) => m.at > turnEndedAt);
const goalAfter = await request('mcode/session/goal/get', { sessionId });
const goalState = goalAfter.result?.goal;
const file = fs.existsSync('/tmp/mcode-goal-continuation.txt')
  ? fs.readFileSync('/tmp/mcode-goal-continuation.txt', 'utf8').trim()
  : null;

// The goal does its own work in the turn it kicks off, not after the client's
// turn: the file it was asked to create is the evidence.
check('the goal did the work on its own', file === 'CONTINUED', `file: ${JSON.stringify(file)}`);
check('the goal spent tokens on it', (goalState?.tokensUsed ?? 0) > 0, `tokens: ${goalState?.tokensUsed}, status: ${goalState?.status}`);
console.log(`     (goal status ${goalState?.status}, ${goalState?.turnsUsed} turns, ${goalState?.tokensUsed} tokens)`);
console.log('RESULT:', file === 'CONTINUED' ? 'the native goal works on its own' : 'the goal did not do its work');
process.exit(failures === 0 ? 0 : 1);
child.kill();
process.exit(0);
