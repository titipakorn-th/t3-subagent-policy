#!/usr/bin/env node
// What signals does the native queue give the client?
//
// Enqueues a message while a turn is running and dumps every notification the
// agent sends, so the completion signal can be identified.
//
//   node probe_mcode_queue_signals.mjs [proxy] [launcher]
import { spawn } from 'node:child_process';
import readline from 'node:readline';

const PROXY = process.argv[2] || '/Users/prince/.minimax-code/bin/mcode-acp-proxy.js';
const LAUNCHER = process.argv[3] || '/Users/prince/.minimax-code/releases/0.7.4/.mcode-launcher';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const t0 = Date.now();

const child = spawn(process.execPath, [PROXY, LAUNCHER, 'acp'], { stdio: ['pipe', 'pipe', 'inherit'] });
let nextId = 1;
const pending = new Map();
const lines = [];
const rl = readline.createInterface({ input: child.stdout, crlfDelay: Infinity });
rl.on('line', (line) => {
  let msg;
  try { msg = JSON.parse(line); } catch { return; }
  if (msg.method) {
    const u = msg.params?.update;
    const isChunk = msg.method === 'session/update' && u?.sessionUpdate === 'agent_message_chunk';
    const text = isChunk ? (u.content?.text ?? (u.content?.content ?? []).map((x) => x.text ?? '').join('')) : '';
    lines.push({ at: Date.now() - t0, method: msg.method, detail: isChunk ? text.slice(0, 60) : JSON.stringify(msg.params).slice(0, 220) });
    return;
  }
  if (msg.id !== undefined && msg.id !== null) {
    lines.push({ at: Date.now() - t0, method: `response#${msg.id}`, detail: JSON.stringify(msg.result ?? msg.error).slice(0, 120) });
    const p = pending.get(msg.id);
    if (p) { pending.delete(msg.id); p(msg); }
  }
});
const request = (method, params) => new Promise((resolve) => {
  const id = nextId++;
  pending.set(id, resolve);
  child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
});

await request('initialize', {
  protocolVersion: 1,
  clientCapabilities: { _meta: { 'minimax-code/extensions': { version: 1, notifications: true } } },
});
const created = await request('session/new', { cwd: process.cwd(), mcpServers: [] });
const sessionId = created.result.sessionId;

// A turn that runs for a while.
request('session/prompt', { sessionId, prompt: [{ type: 'text', text: 'Use the bash tool to run exactly: sleep 15; echo DONE . Then reply DONE.' }] });
await sleep(3000);

// Enqueue a message into the native queue.
const enq = await request('mcode/session/queue/enqueue', { sessionId, text: 'Queued message for the next turn.' });
console.log('enqueue:', JSON.stringify(enq.result ?? enq.error));
const list = await request('mcode/session/queue/list', { sessionId });
console.log('queue/list:', JSON.stringify(list.result ?? list.error));

await sleep(30000);
console.log('\n=== inbound ===');
for (const l of lines) console.log(`+${String(Math.round(l.at / 1000)).padStart(3)}s ${l.method.padEnd(28)} ${l.detail}`);
child.kill();
process.exit(0);
