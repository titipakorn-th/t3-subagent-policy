#!/usr/bin/env node
// The goal control has to be visible in the client.
//
// The agent has no goal slash command, so the proxy advertises one: the
// command shows up in the client's command list and in the session result.
//
//   node test_mcode_acp_goal_command.mjs [proxy] [launcher]
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
const commandLists = [];
const rl = readline.createInterface({ input: child.stdout, crlfDelay: Infinity });
rl.on('line', (line) => {
  let msg;
  try { msg = JSON.parse(line); } catch { return; }
  if (msg.method === 'session/update') {
    const update = msg.params?.update;
    const list = update?.availableCommands ?? update?.available_commands;
    if (Array.isArray(list)) commandLists.push(list.map((c) => c.name));
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

const slashCommands = created.result?.slashCommands ?? [];
check('the session result advertises the goal command',
  slashCommands.some((c) => c.name === 'goal'), JSON.stringify(slashCommands.map((c) => c.name)));
check('the command has a description',
  slashCommands.find((c) => c.name === 'goal')?.description?.length > 0, '');

await new Promise((r) => setTimeout(r, 3000));
const notified = commandLists.some((names) => names.includes('goal'));
check('the command list notification carries it too', notified, commandLists.map((l) => l.length).join(','));

// The command has to work, not just show up.
const result = await request('session/prompt', {
  sessionId,
  prompt: [{ type: 'text', text: '/goal Verify that the advertised goal command works.' }],
});
check('the advertised command works', result.result?.stopReason === 'end_turn', JSON.stringify(result.result ?? result.error));
const goal = await request('mcode/session/goal/get', { sessionId });
check('it created a goal', Boolean(goal.result?.goal), JSON.stringify(goal.result?.goal?.goalId));
await request('mcode/session/goal/clear', { sessionId });

console.log(failures === 0 ? '\nall checks passed' : `\n${failures} check(s) failed`);
child.kill();
process.exit(failures === 0 ? 0 : 1);
