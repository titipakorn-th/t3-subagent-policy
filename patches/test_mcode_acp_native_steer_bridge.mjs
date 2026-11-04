#!/usr/bin/env node
// Proves the proxy uses the agent's native steer when the client sends a
// message without cancelling the running turn.
//
// The client sends a prompt while a turn runs and does not cancel. The proxy
// must use mcode/session/steer, so the message joins the running turn: the
// turn ends with end_turn (not cancelled) and the agent answers the message.
//
//   node test_mcode_acp_native_steer_bridge.mjs [proxy] [launcher]
import { spawn } from 'node:child_process';
import readline from 'node:readline';

const PROXY = process.argv[2] || '/Users/prince/.minimax-code/bin/mcode-acp-proxy.js';
const LAUNCHER = process.argv[3] || '/Users/prince/.minimax-code/releases/0.7.4/.mcode-launcher';
const LONG = 'Use the bash tool to run exactly: sleep 20; echo LONG_DONE . Then reply with the word DONE.';
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

const first = request('session/prompt', { sessionId, prompt: [{ type: 'text', text: LONG }] });
await sleep(3000);

// No cancel: the message joins the running turn.
const second = request('session/prompt', { sessionId, prompt: [{ type: 'text', text: 'Reply with exactly NATIVE_BRIDGE_OK.' }] });

// The agent cannot act on the message while a tool call is running, so give
// it the time the tool call needs.
let marker = false;
for (let waited = 0; waited < 40000; waited += 500) {
  if (answers.join('').includes('NATIVE_BRIDGE_OK')) { marker = true; break; }
  await sleep(500);
}
check('the agent answered the message it was steered with', marker, answers.join('').slice(-100));

const [r1, r2] = [await first, await second];
check('the original turn reported back', Boolean(r1.result?.stopReason), JSON.stringify(r1.result ?? r1.error));
check('the turn was steered, not cancelled', r1.result?.stopReason === 'end_turn', JSON.stringify(r1.result));
check('the client request was answered', Boolean(r2.result?.stopReason), JSON.stringify(r2.result ?? r2.error));

console.log(failures === 0 ? '\nall checks passed' : `\n${failures} check(s) failed`);
child.kill();
process.exit(failures === 0 ? 0 : 1);
