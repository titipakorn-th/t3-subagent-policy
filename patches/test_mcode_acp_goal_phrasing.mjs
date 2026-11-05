#!/usr/bin/env node
// The natural language goal phrasings the goal-mode plugin used to detect.
//
// The plugin translated them into MCP calls; the proxy now maps them onto the
// agent's native goal API, so removing the plugin did not cost the phrasing.
//
//   node test_mcode_acp_goal_phrasing.mjs [proxy] [launcher]
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

await request('initialize', { protocolVersion: 1, clientCapabilities: {} });
const created = await request('session/new', { cwd: process.cwd(), mcpServers: [] });
const sessionId = created.result.sessionId;

// "Set my goal to ..." starts a goal.
chunks.length = 0;
const set = await request('session/prompt', {
  sessionId,
  prompt: [{ type: 'text', text: 'Set my goal to write the line PHRASING_OK to /tmp/mcode-goal-phrasing.txt and stop.' }],
});
check('the phrasing was answered', !set.error && set.result?.stopReason === 'end_turn', JSON.stringify(set.result ?? set.error));
check('the client saw the goal work', chunks.join('').length > 0, chunks.join('').slice(-120));

// "What is my current goal?" reports it.
chunks.length = 0;
const status = await request('session/prompt', {
  sessionId,
  prompt: [{ type: 'text', text: 'What is my current goal?' }],
});
check('the status phrasing was answered', !status.error && status.result?.stopReason === 'end_turn', JSON.stringify(status.result ?? status.error));
check('the status phrasing reported the goal', chunks.join('').includes('/tmp/mcode-goal-phrasing.txt'), chunks.join('').slice(-140));

// "Pause my goal" pauses it.
const paused = await request('session/prompt', {
  sessionId,
  prompt: [{ type: 'text', text: 'Pause my goal for now.' }],
});
check('the pause phrasing was answered', !paused.error && paused.result?.stopReason === 'end_turn', JSON.stringify(paused.result ?? paused.error));

// "Cancel my goal" clears it.
chunks.length = 0;
const cleared = await request('session/prompt', {
  sessionId,
  prompt: [{ type: 'text', text: 'Cancel my goal.' }],
});
check('the cancel phrasing was answered', !cleared.error && cleared.result?.stopReason === 'end_turn', JSON.stringify(cleared.result ?? cleared.error));
check('the cancel phrasing reported the goal as gone', /cleared|is met|is stopped/i.test(chunks.join('')), chunks.join('').slice(-140));

// An ordinary prompt that mentions a goal is left alone.
chunks.length = 0;
const plain = await request('session/prompt', {
  sessionId,
  prompt: [{ type: 'text', text: 'Explain what a goal is in one sentence.' }],
});
check('an ordinary prompt still reaches the model', chunks.join('').length > 0, chunks.join('').slice(-80));

// A path that contains "/goal" is not a goal command either.
chunks.length = 0;
const path = await request('session/prompt', {
  sessionId,
  prompt: [{ type: 'text', text: 'Read the file /goal/notes.txt and summarise it.' }],
});
check('a path with /goal still reaches the model', chunks.join('').length > 0, chunks.join('').slice(-80));

console.log(failures === 0 ? '\nall checks passed' : `\n${failures} check(s) failed`);
child.kill();
process.exit(failures === 0 ? 0 : 1);
