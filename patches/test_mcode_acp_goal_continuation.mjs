#!/usr/bin/env node
// Checks that a goal runs as visible turns, and that it stops on its own.
//
// The agent's native goal API runs a goal's turns in a child session and never
// projects them to the client, so the work would be invisible in T3. The proxy
// runs the goal as ordinary prompt turns instead, keeps the client's request
// open, and the agent's own text reaches the client while it happens.
//
//   node test_mcode_acp_goal_continuation.mjs [proxy] [fake-agent]
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import readline from 'node:readline';

const PROXY = process.argv[2] || '/Users/prince/.minimax-code/bin/mcode-acp-proxy.js';
const AGENT = process.argv[3] || path.join(path.dirname(new URL(import.meta.url).pathname), 'fake-agent.js');
let failures = 0;

function check(label, ok, detail) {
  console.log(`${ok ? 'ok  ' : 'FAIL'}  ${label}${detail ? ' :: ' + detail : ''}`);
  if (!ok) failures++;
}

// Run a goal against the fake agent and report what the client saw.
async function runGoal(agentTurns, env, style, waitMs, goalText) {
  const callsFile = path.join(os.tmpdir(), `fake-agent-calls-${process.pid}-${agentTurns}-${style || 'marker'}.json`);
  const args = [PROXY, process.execPath, AGENT, 'goal', String(agentTurns)];
  if (style) args.push(style);
  const child = spawn(process.execPath, args, {
    stdio: ['pipe', 'pipe', 'inherit'],
    env: { ...process.env, MCODE_FAKE_AGENT_CALLS: callsFile, ...env },
  });
  let nextId = 1;
  const pending = new Map();
  const chunks = [];
  const rl = readline.createInterface({ input: child.stdout, crlfDelay: Infinity });
  rl.on('line', (line) => {
    let msg;
    try { msg = JSON.parse(line); } catch { return; }
    if (msg.method === 'session/update') {
      const u = msg.params?.update;
      if (u?.sessionUpdate === 'agent_message_chunk') chunks.push(u.content?.text ?? '');
      return;
    }
    if (msg.method) return;
    if (msg.id !== undefined && msg.id !== null) {
      const p = pending.get(msg.id);
      if (p) { pending.delete(msg.id); p(msg); }
    }
  });
  const request = (method, params) => new Promise((resolve) => {
    const id = nextId++;
    pending.set(id, resolve);
    child.stdin.write(JSON.stringify({ jsonrpc: 2, id, method, params }) + '\n');
  });

  await request('initialize', { protocolVersion: 1, clientCapabilities: {} });
  const created = await request('session/new', { cwd: process.cwd(), mcpServers: [] });
  const sessionId = created.result.sessionId;
  const prompt = goalText || '/goal Finish the work.';
  const answered = request('session/prompt', {
    sessionId,
    prompt: [{ type: 'text', text: prompt }],
  });
  // A goal that is still running never answers: waitMs then gives up on it.
  const goal = waitMs
    ? await Promise.race([answered, new Promise((resolve) => setTimeout(() => resolve(null), waitMs))])
    : await answered;
  await new Promise((resolve) => setTimeout(resolve, 200));

  const calls = JSON.parse(fs.readFileSync(callsFile, 'utf8'));
  child.kill();
  fs.rmSync(callsFile, { force: true });
  return { goal, said: chunks.join(''), calls };
}

// 1. A goal the agent meets: every turn reaches the client.
const met = await runGoal(2, {});
check('the goal command was answered', !met.goal.error && met.goal.result?.stopReason === 'end_turn', JSON.stringify(met.goal.result ?? met.goal.error));
check("the agent's own words reached the client", met.said.includes('goal turn 1'), met.said.slice(0, 200));
check('every goal turn reached the client', met.said.includes('goal turn 3'), met.said.slice(0, 300));
check('the goal was reported as met', /Goal met after \d+ turn/.test(met.said), met.said.slice(-120));
const metPrompts = met.calls.filter((m) => m === 'session/prompt').length;
check('the goal ran as several prompt turns', metPrompts === 3, `prompts: ${metPrompts}`);
check('no native goal was left behind', met.calls.includes('mcode/session/goal/clear'), met.calls.join(','));

// 2. A goal the agent never meets: the turn cap stops it and answers the client.
const capped = await runGoal(50, { MCODE_GOAL_MAX_TURNS: '3' });
check('the capped goal was answered', !capped.goal.error && capped.goal.result?.stopReason === 'end_turn', JSON.stringify(capped.goal.result ?? capped.goal.error));
const cappedPrompts = capped.calls.filter((m) => m === 'session/prompt').length;
check('the goal stopped at the turn cap', cappedPrompts === 3, `prompts: ${cappedPrompts}`);
check('the client was told the goal was not met', /without being met/.test(capped.said), capped.said.slice(-120));

// 3. A turn that only mentions the marker in passing must not end the goal.
const passing = await runGoal(50, { MCODE_GOAL_MAX_TURNS: '2' }, 'middle');
check('a passing mention did not end the goal', !/Goal met/.test(passing.said), passing.said.slice(-120));
check('the goal kept working instead', /without being met/.test(passing.said), passing.said.slice(-120));

// 4. A turn that runs past the time budget is cut off.
const slow = await runGoal(50, { MCODE_GOAL_MAX_MS: '2000' }, 'slow');
check('the slow goal was answered', !slow.goal.error && slow.goal.result?.stopReason === 'end_turn', JSON.stringify(slow.goal.result ?? slow.goal.error));
check('the time budget cut the turn off', /time budget ran out/.test(slow.said), slow.said.slice(-160));
check('the agent was cancelled', slow.calls.includes('session/cancel'), slow.calls.join(','));

// 5. A goal that asks for more room gets it, whatever the default is.
const asked = await runGoal(50, { MCODE_GOAL_MAX_MS: '2000' }, 'slow', 5000, '/goal Finish the work, you got 10 minutes to execute');
check('the goal asked for a longer budget', asked.goal === null, JSON.stringify(asked.goal));
check('the default budget did not cut it off', !/time budget ran out/.test(asked.said), asked.said.slice(-160));

// 6. A goal that asks for less room gets that.
const short = await runGoal(50, { MCODE_GOAL_MAX_MS: '600000' }, 'slow', 8000, '/goal Finish the work, you got 2 seconds to execute');
check('the goal asked for a shorter budget', short.goal !== null, JSON.stringify(short.goal));
check('the asked budget cut the turn off', /time budget ran out/.test(short.said), short.said.slice(-160));

// 7. An objective that only mentions a duration keeps the default budget.
const mention = await runGoal(50, { MCODE_GOAL_MAX_MS: '600000' }, 'slow', 4000, '/goal Fix the 2 hours timeout bug in the parser.');
check('a duration in the objective is not a budget', mention.goal === null, JSON.stringify(mention.goal));

console.log(failures === 0 ? '\nall checks passed' : `\n${failures} check(s) failed`);
process.exit(failures === 0 ? 0 : 1);
