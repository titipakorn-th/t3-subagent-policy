#!/usr/bin/env node
// Does the goal work twice?
//
// The native goal API used to run the work in the background while the prompt
// that created it was still going, so the same file was written twice. The
// proxy runs the goal as visible turns instead, and clears any native goal for
// the session, so the work happens once.
//
//   node test_mcode_acp_goal_duplicate.mjs [proxy] [launcher]
import { spawn } from 'node:child_process';
import readline from 'node:readline';
import fs from 'node:fs';

const PROXY = process.argv[2] || '/Users/prince/.minimax-code/bin/mcode-acp-proxy.js';
const LAUNCHER = process.argv[3] || '/Users/prince/.minimax-code/releases/0.7.4/.mcode-launcher';
const FILE = '/tmp/mcode-goal-duplicate.txt';
const OBJECTIVE = 'Append the line WORKED to /tmp/mcode-goal-duplicate.txt using the bash tool, then reply WORKED.';

try { fs.unlinkSync(FILE); } catch {}

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
      messages.push(c?.text ?? (c?.content ?? []).map((x) => x.text ?? '').join(''));
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

await request('session/prompt', { sessionId, prompt: [{ type: 'text', text: `/goal ${OBJECTIVE}` }] });

// The goal is done: wait, and see whether a second, invisible worker writes
// the file again.
await new Promise((r) => setTimeout(r, 90000));
const lines = fs.existsSync(FILE) ? fs.readFileSync(FILE, 'utf8').trim().split('\n').filter(Boolean) : [];
const native = await request('mcode/session/goal/get', { sessionId });
console.log('lines written to the file:', lines.length, JSON.stringify(lines));
console.log('agent messages seen by the client:', messages.length);
console.log('native goal left running:', native.result?.goal?.goalId ?? 'none');
console.log('RESULT:', lines.length <= 1 ? 'no duplicate - the work happened once' : 'DUPLICATE - the work happened twice');
try { fs.unlinkSync(FILE); } catch {}
child.kill();
process.exit(lines.length <= 1 ? 0 : 1);
