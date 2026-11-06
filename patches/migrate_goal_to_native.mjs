#!/usr/bin/env node
// Carries a goal-mode plugin goal over to the agent's native goal API.
//
// The native goal has no plan field, so the plan is appended to the objective -
// the objective is what the runtime injects into every continuation turn.
//
//   node migrate_goal_to_native.mjs [backup-goals.json] [proxy] [launcher]
import { spawn } from 'node:child_process';
import readline from 'node:readline';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { defaultLauncher, defaultProxy } from './mcode-paths.mjs';

const BACKUP = process.argv[2] || path.join(os.homedir(), 'Workspaces/TestSpaces/goal-mode-backup/plugin-mcp-state-goals.json');
const PROXY = process.argv[3] || defaultProxy();
const LAUNCHER = process.argv[4] || defaultLauncher();

const state = JSON.parse(fs.readFileSync(BACKUP, 'utf8'));
const sessionId = state.activeSessionId;
const current = state.sessions[sessionId]?.current;
if (!current) {
  console.error(`no active goal for ${sessionId}`);
  process.exit(1);
}

const plan = current.plan;
let objective = current.objective;
if (plan?.phases?.length) {
  const lines = ['Plan (carried over from the goal-mode plugin, which has no native equivalent):'];
  for (const phase of plan.phases ?? []) {
    lines.push(`- ${phase.id} [${phase.status}] ${phase.name}`);
    for (const task of phase.tasks ?? []) {
      lines.push(`  - ${task.id} [${task.status}] ${task.name}`);
    }
  }
  objective = `${objective}\n\n${lines.join('\n')}`;
}

console.log(`session:   ${sessionId}`);
console.log(`goalId:    ${current.goalId ?? '(none in the plugin record)'}`);
console.log(`objective: ${objective.length} chars, tokenBudget ${current.tokenBudget}`);

const child = spawn(process.execPath, [PROXY, LAUNCHER, 'acp'], { stdio: ['pipe', 'pipe', 'inherit'] });
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

await request('initialize', { protocolVersion: 1, clientCapabilities: {} });

// The goal service only knows sessions that are loaded in this agent, so the
// target session has to be resumed here first.
const loaded = await request('session/load', {
  sessionId,
  cwd: process.env.GOAL_MIGRATION_CWD || path.join(os.homedir(), '.minimax/workspace'),
  mcpServers: [],
});
if (loaded.error) {
  console.error('session/load failed:', JSON.stringify(loaded.error));
} else {
  console.log('session loaded:', JSON.stringify(loaded.result?.sessionId ?? sessionId));
}

const before = await request('mcode/session/goal/get', { sessionId });
if (before.result?.goal) {
  console.error(`a native goal already exists for this session: ${before.result.goal.goalId}`);
  process.exit(1);
}

const created = await request('mcode/session/goal/create', {
  sessionId,
  objective,
  ...(current.tokenBudget ? { tokenBudget: current.tokenBudget } : {}),
});
if (created.error) {
  console.error('create failed:', JSON.stringify(created.error));
  process.exit(1);
}
console.log('\ncreated native goal:', JSON.stringify(created.result?.goal, null, 2).slice(0, 900));

const after = await request('mcode/session/goal/get', { sessionId });
console.log('\nverified:', after.result?.goal?.goalId, '| status', after.result?.goal?.status, '| budget', after.result?.goal?.tokenBudget);
child.kill();
process.exit(0);
