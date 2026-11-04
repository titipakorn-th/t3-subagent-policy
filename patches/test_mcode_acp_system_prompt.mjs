#!/usr/bin/env node
// Checks whether --append-system-prompt-file reaches the agent's system prompt
// when the flag is passed through the ACP proxy (the seam the patch would use).
//
//   node test_mcode_acp_system_prompt.mjs [proxy] [launcher]
import { spawn } from 'node:child_process';
import readline from 'node:readline';
import fs from 'node:fs';

const PROXY = process.argv[2] || '/Users/prince/.minimax-code/bin/mcode-acp-proxy.js';
const LAUNCHER = process.argv[3] || '/Users/prince/.minimax-code/releases/0.7.4/.mcode-launcher';
const MARKER = 'PROXY_SYSTEM_PROMPT_MARKER_9F3A';
const SP_FILE = '/tmp/mcode-append-system-prompt.txt';

fs.writeFileSync(SP_FILE, `${MARKER}: when you see this line in your instructions, answer exactly SP_MARKER_SEEN.\n`);

const child = spawn(process.execPath, [PROXY, LAUNCHER, 'acp', '--append-system-prompt-file', SP_FILE], {
  stdio: ['pipe', 'pipe', 'inherit'],
});
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

await request('session/prompt', {
  sessionId,
  prompt: [{ type: 'text', text: `Do you have an instruction line containing the text ${MARKER}? If yes, reply exactly SP_MARKER_SEEN. If not, reply exactly SP_MARKER_ABSENT.` }],
});

const said = answers.join('');
console.log('agent said:', JSON.stringify(said.slice(-200)));
console.log(said.includes('SP_MARKER_SEEN') ? 'ok   system prompt override reached the agent' : 'FAIL marker not found in the agent output');
child.kill();
process.exit(said.includes('SP_MARKER_SEEN') ? 0 : 1);
