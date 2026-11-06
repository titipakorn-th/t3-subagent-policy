#!/usr/bin/env node
// mcode's internal delegation must not be invisible.
//
// The system prompt tells the agent to use T3 Code's delegate_task, but a
// directive is not a guarantee. When the agent starts children of its own,
// the proxy tells the client, so a second orchestrator is visible instead of
// silent.
//
//   node test_mcode_acp_delegation.mjs [proxy]
import { spawn } from 'node:child_process';
import readline from 'node:readline';
import path from 'node:path';
import { defaultAcp, defaultLauncher, defaultProxy } from './mcode-paths.mjs';

const HERE = path.dirname(new URL(import.meta.url).pathname);
const PROXY = process.argv[2] || defaultProxy();
const FAKE = path.join(HERE, 'fake-agent.js');
let failures = 0;

function check(label, ok, detail) {
  console.log(`${ok ? 'ok  ' : 'FAIL'}  ${label}${detail ? ' :: ' + detail : ''}`);
  if (!ok) failures++;
}

const child = spawn(process.execPath, [PROXY, process.execPath, FAKE, 'delegation', '0'], {
  stdio: ['pipe', 'pipe', 'inherit'],
});
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
await request('session/new', { cwd: process.cwd(), mcpServers: [] });
await new Promise((r) => setTimeout(r, 800));

const said = chunks.join('');
check('the internal delegates are reported to the client', said.includes('2 internal delegates'), said.slice(-160));
check('they are named', said.includes('worker-one') && said.includes('worker-two'), said.slice(-160));
check('the owner is named', said.includes('T3 Code'), said.slice(-160));
check('the raw extension notification is not forwarded', !notifications.includes('mcode/session/delegation_update'), notifications.join(', '));

console.log(failures === 0 ? '\nall checks passed' : `\n${failures} check(s) failed`);
child.kill();
process.exit(failures === 0 ? 0 : 1);
