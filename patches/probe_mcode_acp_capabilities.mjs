#!/usr/bin/env node
// Probes the mcode ACP server the way T3 Code drives it, and dumps what it
// advertises plus what the session methods return.
//
//   node probe_mcode_acp_capabilities.mjs [proxy] [launcher]
import { spawn } from 'node:child_process';
import readline from 'node:readline';

const PROXY = process.argv[2] || '/Users/prince/.minimax-code/bin/mcode-acp-proxy.js';
const LAUNCHER = process.argv[3] || '/Users/prince/.minimax-code/releases/0.7.4/.mcode-launcher';

const child = spawn(process.execPath, [PROXY, LAUNCHER, 'acp'], { stdio: ['pipe', 'pipe', 'inherit'] });
let nextId = 1;
const pending = new Map();
const notes = [];
const rl = readline.createInterface({ input: child.stdout, crlfDelay: Infinity });
rl.on('line', (line) => {
  let msg;
  try { msg = JSON.parse(line); } catch { return; }
  if (msg.id !== undefined && msg.id !== null && msg.method === undefined) {
    const p = pending.get(msg.id);
    if (p) { pending.delete(msg.id); p(msg); }
    return;
  }
  if (msg.method) notes.push(`${msg.method}`);
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
    elicitation: {},
  },
});
console.log('=== initialize result ===');
console.log(JSON.stringify(init.result ?? init.error, null, 2));

const auth = await request('authenticate', { methodId: 'anthropic' });
console.log('\n=== authenticate (anthropic) ===');
console.log(JSON.stringify(auth.error ?? auth.result).slice(0, 300));

for (const method of ['providers/list']) {
  const res = await request(method, {});
  console.log(`\n=== ${method} ===`);
  console.log(JSON.stringify(res.error ?? res.result).slice(0, 600));
}

const created = await request('session/new', { cwd: process.cwd(), mcpServers: [] });
console.log('\n=== session/new ===');
const sessionId = created.result?.sessionId;
console.log('sessionId:', sessionId);
const configOptions = created.result?.configOptions ?? [];
console.log('configOptions categories:', JSON.stringify(configOptions.map((c) => ({ id: c.id, category: c.category, type: c.type }))));
console.log('models:', JSON.stringify(created.result?.models)?.slice(0, 400));
console.log('slashCommands:', JSON.stringify((created.result?.slashCommands ?? []).map((c) => c.name)));

const list = await request('session/list', {});
console.log('\n=== session/list ===');
console.log(JSON.stringify(list.error ?? list.result).slice(0, 300));

console.log('\n=== agent->client notifications seen ===');
console.log([...new Set(notes)].join(', ') || '(none)');
child.kill();
process.exit(0);
