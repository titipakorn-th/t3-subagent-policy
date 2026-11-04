#!/usr/bin/env node
// A client prompt sent while a goal turn is running must survive.
//
// The goal owns the session, so the prompt is rejected with "Session already
// has an active Turn.". The proxy has to keep retrying until the goal's turn
// ends and the prompt is accepted.
//
//   node test_mcode_acp_goal_prompt_collision.mjs [proxy] [launcher]
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
const rl = readline.createInterface({ input: child.stdout, crlfDelay: Infinity });
rl.on('line', (line) => {
  let msg;
  try { msg = JSON.parse(line); } catch { return; }
  if (msg.method === 'session/update') {
    const u = msg.params?.update;
    if (u?.sessionUpdate === 'agent_message_chunk') {
      const c = u.content;
      const txt = c?.text ?? (c?.content ?? []).map((x) => x.text ?? '').join('');
      if (txt) messages.push(txt);
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

// A goal whose work takes a while, so the session stays busy.
await request('session/prompt', {
  sessionId,
  prompt: [{ type: 'text', text: '/goal Wait until /tmp/mcode-goal-collision.txt exists, then create it with the word COLLISION and verify it by reading it back.' }],
});

// A prompt the client sends while the goal turn is running.
const started = Date.now();
const collided = request('session/prompt', {
  sessionId,
  prompt: [{ type: 'text', text: 'Reply with exactly CLIENT_PROMPT_DELIVERED.' }],
});
const result = await collided;
const waited = Math.round((Date.now() - started) / 1000);

check('the collided prompt was answered', !result.error, result.error ? JSON.stringify(result.error) : JSON.stringify(result.result));
check('it was not answered with an error', !result.error, result.error ? String(result.error.message) : '');
check('the client prompt reached the model', messages.join('').includes('CLIENT_PROMPT_DELIVERED'), messages.join('').slice(-120));
console.log(`     (the prompt waited ${waited}s for the goal turn to end)`);

console.log(failures === 0 ? '\nall checks passed' : `\n${failures} check(s) failed`);
child.kill();
process.exit(failures === 0 ? 0 : 1);
