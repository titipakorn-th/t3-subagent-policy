#!/usr/bin/env node
// The proxy prefers the agent's native steer. When the agent refuses it, the
// proxy has to fall back to holding the prompt and sending it once the
// running turn has ended - and both client requests must still get an answer.
//
//   node test_mcode_acp_steer_fallback.mjs [proxy]
import { spawn } from 'node:child_process';
import readline from 'node:readline';
import path from 'node:path';

const HERE = path.dirname(new URL(import.meta.url).pathname);
const PROXY = process.argv[2] || '/Users/prince/.minimax-code/bin/mcode-acp-proxy.js';
const FAKE = path.join(HERE, 'fake-agent.js');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let failures = 0;

function check(label, ok, detail) {
  console.log(`${ok ? 'ok  ' : 'FAIL'}  ${label}${detail ? ' :: ' + detail : ''}`);
  if (!ok) failures++;
}

const child = spawn(process.execPath, [PROXY, process.execPath, FAKE, 'advertise-refuse-steer', '0'], {
  stdio: ['pipe', 'pipe', 'inherit'],
});
let nextId = 1;
const pending = new Map();
const rl = readline.createInterface({ input: child.stdout, crlfDelay: Infinity });
rl.on('line', (line) => {
  let msg;
  try { msg = JSON.parse(line); } catch { return; }
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
const withTimeout = (promise, limit) => Promise.race([promise, sleep(limit).then(() => ({ timedOut: true }))]);

await request('initialize', { protocolVersion: 1, clientCapabilities: {} });
const created = await request('session/new', { cwd: process.cwd(), mcpServers: [] });
const sessionId = created.result.sessionId;

// The first turn is running.
const first = request('session/prompt', { sessionId, prompt: [{ type: 'text', text: 'first' }] });
await sleep(300);

// The T3 force send: cancel and the next prompt back to back. The agent
// refuses the native steer, so the proxy has to fall back.
child.stdin.write(JSON.stringify({ jsonrpc: '2.0', method: 'session/cancel', params: { sessionId } }) + '\n');
const second = request('session/prompt', { sessionId, prompt: [{ type: 'text', text: 'second' }] });

const [r1, r2] = [await withTimeout(first, 15000), await withTimeout(second, 15000)];
check('first request answered', !r1.timedOut, r1.timedOut ? 'no response' : JSON.stringify(r1.result ?? r1.error));
check('second request answered', !r2.timedOut, r2.timedOut ? 'no response' : JSON.stringify(r2.result ?? r2.error));
check('second request was not an error', !r2.error, r2.error ? JSON.stringify(r2.error) : '');
check('the fallback ran the held prompt', r2.result?.stopReason === 'end_turn', JSON.stringify(r2.result));

console.log(failures === 0 ? '\nall checks passed' : `\n${failures} check(s) failed`);
child.kill();
process.exit(failures === 0 ? 0 : 1);
